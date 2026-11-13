const line = document.getElementById('popup-line');
const progress = document.getElementById('popup-progress');
const percent = document.getElementById('popup-percent');
const status = document.getElementById('popup-status');

function render(state = {}) {
  line.textContent = state.text || '暂无当前行';
  progress.textContent = state.showLineProgress === false ? '' : (state.lineProgress || '—');
  percent.textContent = state.showPercent === false ? '' : `${Math.round(Number(state.percent || 0))}%`;
  status.textContent = state.ok ? `${state.bookTitle || 'WeFish'} · ${state.chapterName || '正在阅读'}` : '需要先启动 WeFish 桌面端。';
}

function action(name) {
  chrome.runtime.sendMessage({ type: 'action', action: name }).then(render).catch(() => {});
}

document.querySelectorAll('[data-action]').forEach((button) => {
  button.addEventListener('click', () => action(button.dataset.action));
});

document.getElementById('open-sidepanel').addEventListener('click', async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab) await chrome.sidePanel.open({ windowId: tab.windowId });
  window.close();
});

document.getElementById('toggle-overlay').addEventListener('click', async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const result = await chrome.runtime.sendMessage({ type: 'toggle-overlay', tabId: tab?.id });
  status.textContent = result?.ok ? '网页浮层已切换。' : (result?.error || '当前页面不支持浮层。');
});

chrome.runtime.onMessage.addListener((message) => {
  if (message?.type === 'wefish-state') render(message.state);
});

chrome.runtime.sendMessage({ type: 'get-state' }).then(render).catch(() => render());
setInterval(() => chrome.runtime.sendMessage({ type: 'get-state' }).then(render).catch(() => {}), 1000);
