const sideLine = document.getElementById('side-line');
const sideProgress = document.getElementById('side-progress');
const sidePercent = document.getElementById('side-percent');
const ring = document.getElementById('ring-value');
const ringWrap = document.getElementById('ring-wrap');
const book = document.getElementById('book');
const dot = document.getElementById('status-dot');

function render(state = {}) {
  const value = Math.max(0, Math.min(100, Number(state.percent || 0)));
  sideLine.textContent = state.text || '暂无当前行';
  sideProgress.textContent = state.showLineProgress === false ? '' : (state.lineProgress || '—');
  sidePercent.textContent = `${Math.round(value)}%`;
  book.textContent = state.ok ? `${state.bookTitle || 'WeFish'} · ${state.chapterName || '正在阅读'}` : '等待 WeFish 桌面端';
  dot.classList.toggle('ready', Boolean(state.ok));
  ringWrap.hidden = state.showPercent === false;
  const circumference = 2 * Math.PI * 21;
  ring.style.strokeDasharray = String(circumference);
  ring.style.strokeDashoffset = String(circumference * (1 - value / 100));
  ring.setAttribute('aria-valuenow', String(Math.round(value)));
}

function action(name) {
  chrome.runtime.sendMessage({ type: 'action', action: name }).then(render).catch(() => {});
}

document.querySelectorAll('[data-action]').forEach((button) => {
  button.addEventListener('click', () => action(button.dataset.action));
});
chrome.runtime.onMessage.addListener((message) => {
  if (message?.type === 'wefish-state') render(message.state);
});
chrome.runtime.sendMessage({ type: 'get-state' }).then(render).catch(() => render());
setInterval(() => chrome.runtime.sendMessage({ type: 'get-state' }).then(render).catch(() => {}), 1000);
