const WEFISH_URL = 'http://127.0.0.1:27183';
const POLL_MINUTES = 1 / 60;
let lastState = {
  ok: false,
  text: '请先启动 WeFish',
  rawText: '',
  lineProgress: '',
  percent: 0,
  showLineProgress: true,
  showPercent: true,
};

async function refreshState() {
  try {
    const response = await fetch(`${WEFISH_URL}/api/reader-state`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`WeFish ${response.status}`);
    lastState = await response.json();
  } catch (_) {
    lastState = {
      ...lastState,
      ok: false,
      text: 'WeFish 未连接',
      rawText: '',
    };
  }
  await chrome.storage.local.set({ wefishState: lastState });
  broadcastState();
  updateBadge();
  return lastState;
}

function updateBadge() {
  const percent = Number(lastState.percent || 0);
  chrome.action.setBadgeBackgroundColor({ color: '#18221d' });
  chrome.action.setBadgeText({ text: lastState.ok && lastState.showPercent !== false ? `${Math.round(percent)}` : '' });
}

function broadcastState() {
  chrome.runtime.sendMessage({ type: 'wefish-state', state: lastState }).catch(() => {});
  chrome.tabs.query({}).then((tabs) => {
    tabs.forEach((tab) => chrome.tabs.sendMessage(tab.id, { type: 'wefish-state', state: lastState }).catch(() => {}));
  }).catch(() => {});
}

async function sendAction(action) {
  try {
    const response = await fetch(`${WEFISH_URL}/api/reader-action`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action }),
    });
    if (!response.ok) throw new Error(`WeFish ${response.status}`);
    await new Promise((resolve) => setTimeout(resolve, 80));
    return refreshState();
  } catch (_) {
    return refreshState();
  }
}

async function toggleOverlay(tabId) {
  if (!tabId) return { ok: false, error: '没有可用的网页标签' };
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ['overlay.js'] });
    return { ok: true };
  } catch (error) {
    return { ok: false, error: error.message || '当前页面不支持浮层' };
  }
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: false }).catch(() => {});
  chrome.alarms.create('wefish-state-poll', { periodInMinutes: POLL_MINUTES });
  refreshState();
});

chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create('wefish-state-poll', { periodInMinutes: POLL_MINUTES });
  refreshState();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'wefish-state-poll') refreshState();
});

chrome.commands.onCommand.addListener((command) => {
  const actions = {
    'next-line': 'line-next',
    'previous-line': 'line-previous',
    'toggle-reading': 'line-toggle',
  };
  if (actions[command]) sendAction(actions[command]);
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === 'get-state') {
    refreshState().then(sendResponse);
    return true;
  }
  if (message?.type === 'action') {
    sendAction(String(message.action || '')).then(sendResponse);
    return true;
  }
  if (message?.type === 'toggle-overlay') {
    toggleOverlay(message.tabId || sender.tab?.id).then(sendResponse);
    return true;
  }
  if (message?.type === 'wefish-state') {
    lastState = message.state || lastState;
  }
  return false;
});

refreshState();
