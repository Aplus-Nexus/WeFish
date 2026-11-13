const { app, BrowserWindow, Tray, Menu, nativeImage, globalShortcut, ipcMain, screen, dialog, clipboard } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const http = require('http');
const path = require('path');

const mockMode = process.argv.includes('--mock');
const selfTestMode = process.argv.includes('--self-test');
const showOnStart = process.argv.includes('--show');
const hasInstanceLock = app.requestSingleInstanceLock();
if (!hasInstanceLock) {
  // A second launch must terminate immediately. Leaving this process alive
  // without a window makes macOS report an unresponsive application.
  app.quit();
}
const isWindows = process.platform === 'win32';
const SHORTCUTS = Object.freeze({
  togglePanel: 'CommandOrControl+Alt+M',
  sync: 'CommandOrControl+Alt+R',
  pagePrevious: 'CommandOrControl+Alt+Left',
  pageNext: 'CommandOrControl+Alt+Right',
  chapterPrevious: 'CommandOrControl+Alt+Shift+Left',
  chapterNext: 'CommandOrControl+Alt+Shift+Right',
  linePrevious: 'CommandOrControl+Alt+Up',
  lineNext: 'CommandOrControl+Alt+Down',
  lineToggle: 'CommandOrControl+Alt+Space',
  help: 'CommandOrControl+Alt+H',
  compact: 'CommandOrControl+Alt+P',
  officeTitle: 'CommandOrControl+Alt+T',
});
let panel = null;
let miniSurface = null;
let thoughtWindow = null;
let tray = null;
let progressTray = null;
let progressTrayHasRing = false;
let trayMenu = null;
let bridge = null;
let bridgeUrl = '';
let stopping = false;
let officeTitleMode = false;
let officeTitleMenuItem = null;
let compactMode = isWindows;
let compactMenuItem = null;
let quitInProgress = false;
let trayLineText = '';
let trayProgressTitle = '';
const INTEGRATION_PORT = Math.max(1024, Number(process.env.WEFISH_INTEGRATION_PORT) || 27183);
let integrationServer = null;
let panelPlaced = false;
let rendererReady = Promise.resolve();
let miniSurfaceReady = Promise.resolve();
let miniSnapshot = { text: '', rawText: '', widthMode: 'dynamic', widthChars: 28 };
let currentContext = {
  line: '',
  paragraph: '',
  bookId: '',
  bookTitle: '',
  chapterUid: '',
  chapterName: '',
  range: '0-0',
  paragraphRange: '0-0',
};

function trayImage() {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 22 22">
    <circle cx="11" cy="11" r="9" fill="#B8F27C"/><circle cx="11" cy="11" r="4" fill="#101413"/>
  </svg>`;
  const image = nativeImage.createFromDataURL(`data:image/svg+xml;base64,${Buffer.from(svg).toString('base64')}`);
  if (process.platform === 'darwin') image.setTemplateImage(true);
  return image;
}

function positionPanel(force = false) {
  if (!panel) return;
  if (panelPlaced && !force) return;
  const display = screen.getPrimaryDisplay();
  const work = display.workArea;
  const width = 390;
  const height = !isWindows && compactMode ? 270 : 650;
  panel.setBounds({ x: work.x + work.width - width - 14, y: work.y + 12, width, height }, false);
  panelPlaced = true;
}

function setPanelVisible(visible) {
  if (!panel) return;
  if (visible) {
    positionPanel();
    panel.showInactive();
    panel.setAlwaysOnTop(true, 'floating');
    panel.webContents.send('panel-visibility', true);
  } else {
    panel.webContents.send('panel-visibility', false);
    panel.hide();
  }
}

function togglePanel() {
  setPanelVisible(!panel || !panel.isVisible());
}

function dispatchCommand(command) {
  if (!panel || panel.isDestroyed()) return;
  const send = () => {
    if (panel && !panel.isDestroyed()) panel.webContents.send('command', command);
  };
  if (panel.webContents.isLoading()) rendererReady.then(send).catch(() => {});
  else send();
}

function miniSurfaceBounds(snapshot = miniSnapshot) {
  const display = screen.getPrimaryDisplay();
  const work = display.workArea;
  const widthChars = Math.max(12, Math.min(120, Number(snapshot.widthChars) || 28));
  const textChars = Math.max(12, Array.from(String(snapshot.text || '')).length);
  const chars = snapshot.widthMode === 'fill'
    ? widthChars
    : snapshot.widthMode === 'fixed'
      ? widthChars
      : textChars;
  const maxWidth = Math.max(180, work.width - 28);
  const width = Math.min(maxWidth, Math.max(180, 36 + chars * 16));
  const height = 68;
  return {
    x: work.x + work.width - width - 14,
    y: work.y + work.height - height - 12,
    width,
    height,
  };
}

function setMiniSurfaceVisible(visible) {
  if (!isWindows || !miniSurface || miniSurface.isDestroyed()) return;
  if (visible && miniSnapshot.text) {
    miniSurface.setAlwaysOnTop(true);
    miniSurface.setBounds(miniSurfaceBounds(), false);
    miniSurface.showInactive();
    miniSurface.setAlwaysOnTop(true);
    miniSurface.webContents.send('mini-update', miniSnapshot);
  } else {
    miniSurface.hide();
  }
}

function updateMiniSurface(value) {
  const payload = value && typeof value === 'object' ? value : { text: value };
  miniSnapshot = {
    text: String(payload.text || ''),
    rawText: String(payload.rawText || payload.text || ''),
    widthMode: ['dynamic', 'fixed', 'fill'].includes(payload.widthMode) ? payload.widthMode : 'dynamic',
    widthChars: Math.max(12, Math.min(120, Math.round(Number(payload.widthChars) || 28))),
    lineProgress: String(payload.lineProgress || ''),
    percent: Number(payload.percent || 0),
    ringPng: String(payload.ringPng || ''),
    showLineProgress: payload.showLineProgress !== false,
    showPercent: payload.showPercent !== false,
  };
  if (!isWindows || !miniSurface || miniSurface.isDestroyed()) return;
  miniSurfaceReady.then(() => {
    if (!miniSurface || miniSurface.isDestroyed()) return;
    miniSurface.setBounds(miniSurfaceBounds(), false);
    miniSurface.webContents.send('mini-update', miniSnapshot);
    setMiniSurfaceVisible(compactMode);
  }).catch(() => {});
}

function setOfficeTitleMode(enabled) {
  officeTitleMode = Boolean(enabled);
  panel?.setTitle(officeTitleMode ? '工作文档' : '阅读器');
  tray?.setToolTip(officeTitleMode ? '工作文档' : '阅读器');
  if (officeTitleMenuItem) officeTitleMenuItem.checked = officeTitleMode;
  if (!officeTitleMode && !isWindows) renderTrayTitle();
}

function progressRingImage(value, visible = true) {
  const percent = Math.max(0, Math.min(100, Number(value) || 0));
  const center = 14;
  const radius = 10.5;
  const angle = (-90 + (percent / 100) * 360) * Math.PI / 180;
  const endX = center + radius * Math.cos(angle);
  const endY = center + radius * Math.sin(angle);
  const largeArc = percent > 50 ? 1 : 0;
  const arc = percent <= 0
    ? ''
    : percent >= 99.9
      ? `<circle cx="${center}" cy="${center}" r="${radius}" fill="none" stroke="#B8F27C" stroke-width="3.6"/>`
      : `<path d="M ${center} ${center - radius} A ${radius} ${radius} 0 ${largeArc} 1 ${endX.toFixed(3)} ${endY.toFixed(3)}" fill="none" stroke="#B8F27C" stroke-width="3.6" stroke-linecap="round"/>`;
  const svg = visible
    ? `<svg xmlns="http://www.w3.org/2000/svg" width="28" height="28" viewBox="0 0 28 28">
        <circle cx="${center}" cy="${center}" r="${radius}" fill="none" stroke="#59645F" stroke-width="3.6"/>
        ${arc}
        <text x="${center}" y="${center + 2.5}" text-anchor="middle" fill="#B8F27C" font-family="-apple-system, sans-serif" font-size="7" font-weight="700">${Math.round(percent)}</text>
      </svg>`
    : '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1" viewBox="0 0 1 1"><path fill="none" d="M0 0h1v1H0z"/></svg>';
  const image = nativeImage.createFromDataURL(`data:image/svg+xml;base64,${Buffer.from(svg).toString('base64')}`);
  if (typeof image.setTemplateImage === 'function') image.setTemplateImage(false);
  return image;
}

function updateProgressTray() {
  if (!progressTray || progressTray.isDestroyed()) return;
  let image = null;
  if (miniSnapshot.showPercent !== false && miniSnapshot.ringPng) {
    image = nativeImage.createFromDataURL(miniSnapshot.ringPng);
  }
  if (!image || image.isEmpty()) {
    image = progressRingImage(miniSnapshot.percent, miniSnapshot.showPercent !== false);
  }
  progressTray.setImage(image);
  progressTrayHasRing = miniSnapshot.showPercent !== false && !image.isEmpty();
  progressTray.setToolTip(
    miniSnapshot.showPercent === false
      ? '阅读进度已隐藏'
      : `${miniSnapshot.lineProgress || '阅读进度'} · ${Math.round(Number(miniSnapshot.percent) || 0)}%`,
  );
}

function renderTrayTitle() {
  if (!tray || officeTitleMode) return;
  const title = `${trayLineText}${trayProgressTitle}`.trim();
  if (process.platform === 'darwin' && typeof tray.setTitle === 'function') {
    tray.setTitle(title);
  }
  tray.setToolTip(process.platform === 'darwin' ? (title || '阅读器') : '阅读器');
}

function setTrayLine(value) {
  const payload = value && typeof value === 'object' ? value : { text: value };
  // Keep the renderer's non-breaking padding: macOS uses the title width to
  // reserve the visible menu-bar reading area.
  trayLineText = String(payload.text || '')
    .replace(/[\t\n\r ]+/g, ' ')
    .replace(/^ +| +$/g, '')
    .slice(0, 160);
  if (value && typeof value === 'object') {
    const progressParts = [];
    if (payload.showLineProgress !== false && payload.lineProgress) {
      progressParts.push(String(payload.lineProgress).replace(/\s+/g, ' ').trim());
    }
    trayProgressTitle = progressParts.length ? `  ·  ${progressParts.join('  ·  ')}` : '';
    currentContext = {
      ...currentContext,
      line: String(payload.rawText || payload.text || '').trim(),
      paragraph: String(payload.paragraph || payload.rawText || payload.text || '').trim(),
      bookId: String(payload.bookId || currentContext.bookId || ''),
      bookTitle: String(payload.bookTitle || currentContext.bookTitle || ''),
      chapterUid: String(payload.chapterUid ?? currentContext.chapterUid ?? ''),
      chapterName: String(payload.chapterName || currentContext.chapterName || ''),
      range: String(payload.range || currentContext.range || '0-0'),
      paragraphRange: String(payload.paragraphRange || currentContext.paragraphRange || payload.range || '0-0'),
    };
  }
  updateMiniSurface({ ...payload, text: String(payload.text || '') });
  updateProgressTray();
  renderTrayTitle();
}

function setCompactMode(enabled) {
  compactMode = Boolean(enabled);
  if (isWindows) {
    setMiniSurfaceVisible(compactMode);
    if (compactMenuItem) compactMenuItem.checked = compactMode;
    return;
  }
  if (panel) {
    panel.setMinimumSize(390, 270);
    panel.setSize(390, compactMode ? 270 : 650);
    panel.setOpacity(compactMode ? 0.88 : 1);
    positionPanel();
  }
  if (compactMenuItem) compactMenuItem.checked = compactMode;
}

function createPanel() {
  panel = new BrowserWindow({
    width: 390,
    height: 650,
    minWidth: 390,
    maxWidth: 390,
    minHeight: 270,
    maxHeight: 760,
    show: false,
    frame: false,
    resizable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    backgroundColor: '#101413',
    title: '阅读器',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  });
  rendererReady = new Promise((resolve) => panel.webContents.once('did-finish-load', resolve));
  if (showOnStart) panel.webContents.once('did-finish-load', () => setPanelVisible(true));
  panel.loadFile(path.join(__dirname, 'renderer', 'index.html'), { hash: selfTestMode ? 'self-test' : '' });
  panel.on('blur', () => {
    // The compact capsule is the stealth reading surface: it must remain in
    // the corner while the user works in another app. The full reader keeps
    // the convenient click-away dismissal behavior.
    if (panel && panel.isVisible() && !isWindows && !compactMode && !panel.webContents.isDevToolsOpened()) setPanelVisible(false);
  });
  panel.on('closed', () => { panel = null; });
}

function createThoughtWindow() {
  thoughtWindow = new BrowserWindow({
    width: 390,
    height: 250,
    minWidth: 340,
    minHeight: 220,
    maxWidth: 520,
    maxHeight: 420,
    show: false,
    frame: false,
    resizable: true,
    skipTaskbar: true,
    alwaysOnTop: true,
    backgroundColor: '#101413',
    title: '写想法',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  });
  thoughtWindow.loadFile(path.join(__dirname, 'renderer', 'thought.html'));
  thoughtWindow.on('closed', () => { thoughtWindow = null; });
}

function openThoughtWindow() {
  if (!thoughtWindow || thoughtWindow.isDestroyed()) createThoughtWindow();
  const show = () => {
    if (!thoughtWindow || thoughtWindow.isDestroyed()) return;
    thoughtWindow.webContents.send('thought-context', currentContext);
    thoughtWindow.show();
    thoughtWindow.focus();
  };
  if (thoughtWindow.webContents.isLoading()) thoughtWindow.webContents.once('did-finish-load', show);
  else show();
}

function copyCurrentContent(kind) {
  const value = kind === 'paragraph' ? currentContext.paragraph : currentContext.line;
  if (!value) return;
  clipboard.writeText(value);
}

function createWindowsReadingSurface() {
  if (!isWindows) return;
  miniSurface = new BrowserWindow({
    width: 360,
    height: 68,
    show: false,
    frame: false,
    transparent: true,
    resizable: false,
    movable: false,
    focusable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    hasShadow: false,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  });
  miniSurface.setIgnoreMouseEvents(true, { forward: true });
  miniSurfaceReady = new Promise((resolve) => miniSurface.webContents.once('did-finish-load', resolve));
  miniSurface.loadFile(path.join(__dirname, 'renderer', 'mini.html'));
  miniSurface.on('closed', () => { miniSurface = null; });
}

function createTray() {
  tray = new Tray(trayImage());
  tray.setToolTip('阅读器');
  tray.on('click', togglePanel);
  const menu = Menu.buildFromTemplate([
    { label: '显示 / 隐藏阅读舱', accelerator: SHORTCUTS.togglePanel, click: togglePanel },
    { type: 'separator' },
    { label: '同步书架', accelerator: SHORTCUTS.sync, click: () => dispatchCommand('sync') },
    { label: '上一页', accelerator: SHORTCUTS.pagePrevious, click: () => dispatchCommand('page-previous') },
    { label: '下一页', accelerator: SHORTCUTS.pageNext, click: () => dispatchCommand('page-next') },
    { label: '上一章', accelerator: SHORTCUTS.chapterPrevious, click: () => dispatchCommand('chapter-previous') },
    { label: '下一章', accelerator: SHORTCUTS.chapterNext, click: () => dispatchCommand('chapter-next') },
    { label: '上一行', accelerator: SHORTCUTS.linePrevious, click: () => dispatchCommand('line-previous') },
    { label: '下一行', accelerator: SHORTCUTS.lineNext, click: () => dispatchCommand('line-next') },
    { label: '自动逐行', accelerator: SHORTCUTS.lineToggle, click: () => dispatchCommand('line-toggle') },
    { label: '当前内容', submenu: [
      { label: '复制当前行', click: () => copyCurrentContent('line') },
      { label: '复制当前段', click: () => copyCurrentContent('paragraph') },
      { label: '划线当前行', click: () => dispatchCommand('highlight-line') },
      { label: '划线当前段', click: () => dispatchCommand('highlight-paragraph') },
      { label: '写想法', click: openThoughtWindow },
    ] },
    { label: '阅读进度显示', submenu: [
      { label: '显示 / 隐藏行数', click: () => dispatchCommand('progress-lines:toggle') },
      { label: '显示 / 隐藏百分比', click: () => dispatchCommand('progress-percent:toggle') },
    ] },
    { label: '逐行速度', submenu: [
      { label: '0.5 秒（快）', click: () => dispatchCommand('line-interval:0.5') },
      { label: '1 秒（默认）', click: () => dispatchCommand('line-interval:1') },
      { label: '2 秒', click: () => dispatchCommand('line-interval:2') },
      { label: '3 秒', click: () => dispatchCommand('line-interval:3') },
      { label: '5 秒（慢）', click: () => dispatchCommand('line-interval:5') },
    ] },
    { label: '顶部栏阅读区域', submenu: [
      { label: '跟随文本（动态）', click: () => dispatchCommand('tray-width:dynamic') },
      { label: '固定 28 字', click: () => dispatchCommand('tray-width:fixed:28') },
      { label: '固定 40 字', click: () => dispatchCommand('tray-width:fixed:40') },
      { label: '固定 56 字', click: () => dispatchCommand('tray-width:fixed:56') },
      { label: '尽量占满空余位置', click: () => dispatchCommand('tray-width:fill') },
      { type: 'separator' },
      { label: '打开宽度设置', click: () => { setPanelVisible(true); dispatchCommand('tray-width:settings'); } },
    ] },
    { label: '快捷键与使用说明', accelerator: SHORTCUTS.help, click: () => { setPanelVisible(true); dispatchCommand('help'); } },
    { label: '微信扫码', click: () => { setPanelVisible(true); dispatchCommand('qr'); } },
    { id: 'compact-mode', label: '小窗模式', accelerator: SHORTCUTS.compact, type: 'checkbox', click: (item) => setCompactMode(item.checked) },
    { id: 'office-title-mode', label: '工作文档标题', accelerator: SHORTCUTS.officeTitle, type: 'checkbox', click: (item) => setOfficeTitleMode(item.checked) },
    { type: 'separator' },
    { label: '关于 / 许可证', click: () => dialog.showMessageBox(panel || undefined, {
      type: 'info',
      title: 'WeFish 桌面阅读器',
      message: 'WeFish 桌面阅读器',
      detail: '本程序基于 finlater/weread.koplugin 的协议处理与内容解码思路改编。\n\n本项目及改编部分按 GNU Affero General Public License 3.0 发布；不提供任何保证。安装包内的 licenses/LICENSE 与 licenses/NOTICE 保存完整许可证和来源说明，项目源码包含对应源码。\n\nWeFish 与微信读书、腾讯或 KOReader 没有隶属、背书或赞助关系。',
      buttons: ['知道了'],
    }) },
    { label: '退出阅读器', click: () => app.quit() },
  ]);
  compactMenuItem = menu.getMenuItemById('compact-mode');
  officeTitleMenuItem = menu.getMenuItemById('office-title-mode');
  compactMenuItem.checked = compactMode;
  trayMenu = menu;
  tray.setContextMenu(menu);
}

function createProgressTray() {
  // Electron exposes the macOS status item image separately from its title.
  // A second status item keeps the real ring graphical and lets it sit at the
  // right edge of the WeFish status component instead of using a text glyph.
  if (process.platform !== 'darwin') return;
  progressTray = new Tray(progressRingImage(0));
  progressTray.setToolTip('阅读进度');
  progressTray.on('click', togglePanel);
  if (trayMenu) progressTray.setContextMenu(trayMenu);
}

function integrationPayload() {
  return {
    ok: true,
    app: 'WeFish',
    version: app.getVersion(),
    text: trayLineText,
    rawText: currentContext.line || trayLineText,
    paragraph: currentContext.paragraph || '',
    bookTitle: currentContext.bookTitle || '',
    chapterName: currentContext.chapterName || '',
    lineProgress: miniSnapshot.lineProgress || '',
    percent: Number(miniSnapshot.percent || 0),
    showLineProgress: miniSnapshot.showLineProgress !== false,
    showPercent: miniSnapshot.showPercent !== false,
    connected: Boolean(bridgeUrl),
    updatedAt: Date.now(),
  };
}

function integrationHeaders(response, contentType = 'application/json') {
  response.setHeader('Content-Type', contentType);
  response.setHeader('Access-Control-Allow-Origin', '*');
  response.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  response.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  response.setHeader('Cache-Control', 'no-store');
}

function integrationJson(response, status, payload) {
  const body = Buffer.from(JSON.stringify(payload));
  integrationHeaders(response);
  response.writeHead(status);
  response.end(body);
}

function startIntegrationServer() {
  integrationServer = http.createServer((request, response) => {
    const route = new URL(request.url || '/', `http://127.0.0.1:${INTEGRATION_PORT}`).pathname;
    if (request.method === 'OPTIONS') {
      integrationHeaders(response);
      response.writeHead(204);
      response.end();
      return;
    }
    if (request.method === 'GET' && route === '/api/reader-state') {
      integrationJson(response, 200, integrationPayload());
      return;
    }
    if (request.method === 'GET' && route === '/api/health') {
      integrationJson(response, 200, { ok: true, app: 'WeFish', version: app.getVersion() });
      return;
    }
    if (request.method === 'POST' && route === '/api/reader-action') {
      let body = '';
      request.setEncoding('utf8');
      request.on('data', (chunk) => { body += chunk; });
      request.on('end', () => {
        let payload = {};
        try { payload = body ? JSON.parse(body) : {}; } catch (_) { payload = {}; }
        const allowed = new Set(['line-previous', 'line-next', 'line-toggle', 'page-previous', 'page-next', 'toggle-panel']);
        const action = String(payload.action || '');
        if (!allowed.has(action)) {
          integrationJson(response, 400, { ok: false, error: '不支持的阅读操作' });
          return;
        }
        if (action === 'toggle-panel') togglePanel();
        else dispatchCommand(action);
        integrationJson(response, 200, { ok: true, action });
      });
      return;
    }
    integrationJson(response, 404, { ok: false, error: '未知接口' });
  });
  integrationServer.on('error', (error) => {
    integrationServer = null;
    if (process.env.WEFISH_DEBUG) console.error(`Edge integration unavailable: ${error.message}`);
  });
  integrationServer.listen(INTEGRATION_PORT, '127.0.0.1');
}

function bridgeCommand() {
  if (app.isPackaged) {
    const name = process.platform === 'win32' ? 'WeFishService.exe' : 'WeFishService';
    const bundled = path.join(process.resourcesPath, 'WeFishService');
    const local = path.join(app.getPath('userData'), 'WeFishService');
    // Running the PyInstaller directory from inside an app bundle can be
    // blocked by macOS when it contains framework symlinks. Copying the
    // signed resource to the per-user data directory keeps the bundle clean
    // and gives both macOS and Windows a normal executable path.
    const executable = path.join(local, name);
    const marker = path.join(local, '.wefish-source.json');
    const sourceExecutable = path.join(bundled, name);
    let sourceStamp = null;
    try {
      const stat = fs.statSync(sourceExecutable);
      sourceStamp = { version: app.getVersion(), size: stat.size, mtimeMs: Math.floor(stat.mtimeMs) };
    } catch (error) {
      throw new Error(`WeFish service bundle is missing: ${error.message}`);
    }
    let installedStamp = null;
    try {
      installedStamp = JSON.parse(fs.readFileSync(marker, 'utf8'));
    } catch (_) {
      installedStamp = null;
    }
    const stampMatches = installedStamp
      && installedStamp.version === sourceStamp.version
      && installedStamp.size === sourceStamp.size
      && installedStamp.mtimeMs === sourceStamp.mtimeMs;
    if (!stampMatches || !fs.existsSync(executable)) {
      const staged = `${local}.staging-${process.pid}`;
      const previous = `${local}.previous-${process.pid}`;
      fs.rmSync(staged, { recursive: true, force: true });
      fs.rmSync(previous, { recursive: true, force: true });
      fs.cpSync(bundled, staged, { recursive: true, force: true, verbatimSymlinks: true });
      fs.writeFileSync(path.join(staged, '.wefish-source.json'), JSON.stringify(sourceStamp));
      try {
        if (fs.existsSync(local)) fs.renameSync(local, previous);
        fs.renameSync(staged, local);
        fs.rmSync(previous, { recursive: true, force: true });
      } catch (error) {
        // A previous crashed instance may still hold a Windows sidecar file.
        // Keep a working installed copy in that rare case and clean staging.
        fs.rmSync(staged, { recursive: true, force: true });
        if (!fs.existsSync(executable)) throw error;
      }
    }
    return { command: path.join(local, name), args: [] };
  }
  return {
    command: process.env.WEFISH_PYTHON || (process.platform === 'win32' ? 'python' : 'python3'),
    args: [path.join(__dirname, '..', 'wefish_service.py')],
  };
}

function startBridge() {
  return new Promise((resolve, reject) => {
    const command = bridgeCommand();
    const args = [...command.args, '--port', '0'];
    if (mockMode) args.push('--mock');
    const bridgeEnv = { ...process.env };
    if (app.isPackaged && process.platform === 'darwin') {
      // The unsigned PyInstaller sidecar has no stable macOS code identity,
      // so Keychain keeps asking for access on every copied service binary.
      // Keep the session in the app's private, 0600 file store instead.
      bridgeEnv.WEFISH_CREDENTIAL_STORE = 'file';
      bridgeEnv.WEFISH_CREDENTIAL_FILE = path.join(app.getPath('userData'), 'credentials.json');
    }
    bridge = spawn(command.command, args, { cwd: path.join(__dirname, '..'), env: bridgeEnv, stdio: ['ignore', 'pipe', 'pipe'] });
    let output = '';
    const onData = (chunk) => {
      output += chunk.toString();
      const match = output.match(/WEFISH_PORT=(\d+)/);
      if (match && !bridgeUrl) {
        bridgeUrl = `http://127.0.0.1:${match[1]}`;
        resolve(bridgeUrl);
      }
    };
    bridge.stdout.on('data', onData);
    bridge.stderr.on('data', (chunk) => { if (process.env.WEFISH_DEBUG) console.error(chunk.toString()); });
    bridge.on('error', (error) => {
      if (process.env.WEFISH_DEBUG) console.error(`WeFish service spawn error: ${error.message}`);
      reject(error);
    });
    bridge.on('exit', (code) => {
      if (!stopping && !bridgeUrl) {
        const error = new Error(`WeFish service exited (${code})`);
        if (process.env.WEFISH_DEBUG) console.error(error.message);
        reject(error);
      }
    });
  });
}

function registerShortcuts() {
  globalShortcut.register(SHORTCUTS.togglePanel, togglePanel);
  globalShortcut.register(SHORTCUTS.help, () => { setPanelVisible(true); dispatchCommand('help'); });
  globalShortcut.register(SHORTCUTS.sync, () => dispatchCommand('sync'));
  globalShortcut.register(SHORTCUTS.pagePrevious, () => dispatchCommand('page-previous'));
  globalShortcut.register(SHORTCUTS.pageNext, () => dispatchCommand('page-next'));
  globalShortcut.register(SHORTCUTS.chapterPrevious, () => dispatchCommand('chapter-previous'));
  globalShortcut.register(SHORTCUTS.chapterNext, () => dispatchCommand('chapter-next'));
  globalShortcut.register(SHORTCUTS.linePrevious, () => dispatchCommand('line-previous'));
  globalShortcut.register(SHORTCUTS.lineNext, () => dispatchCommand('line-next'));
  globalShortcut.register(SHORTCUTS.lineToggle, () => dispatchCommand('line-toggle'));
  globalShortcut.register(SHORTCUTS.officeTitle, () => setOfficeTitleMode(!officeTitleMode));
  globalShortcut.register(SHORTCUTS.compact, () => setCompactMode(!compactMode));
}

function assertBridgeHealth() {
  return new Promise((resolve, reject) => {
    const request = http.get(`${bridgeUrl}/api/health`, (response) => {
      let body = '';
      response.setEncoding('utf8');
      response.on('data', (chunk) => { body += chunk; });
      response.on('end', () => {
        try {
          const payload = JSON.parse(body);
          if (response.statusCode !== 200 || !payload.ok) {
            reject(new Error(`health check failed (${response.statusCode})`));
            return;
          }
          resolve(payload);
        } catch (error) {
          reject(new Error(`health check returned invalid JSON: ${error.message}`));
        }
      });
    });
    request.setTimeout(5000, () => request.destroy(new Error('health check timed out')));
    request.on('error', reject);
  });
}

function bridgeRequest(method, route, payload = undefined) {
  return new Promise((resolve, reject) => {
    const body = payload === undefined ? '' : JSON.stringify(payload);
    const request = http.request(`${bridgeUrl}${route}`, {
      method,
      headers: payload === undefined ? {} : {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body),
      },
    }, (response) => {
      let body = '';
      response.setEncoding('utf8');
      response.on('data', (chunk) => { body += chunk; });
      response.on('end', () => {
        let value = body;
        try { value = JSON.parse(body); } catch (_) { /* HTML/text response */ }
        if (response.statusCode < 200 || response.statusCode >= 300) {
          const detail = typeof value === 'string' ? value.slice(0, 160) : String(value?.error || 'unknown error').slice(0, 160);
          reject(new Error(`bridge ${route} failed (${response.statusCode}): ${detail}`));
          return;
        }
        resolve(value);
      });
    });
    request.setTimeout(5000, () => request.destroy(new Error(`bridge ${route} timed out`)));
    request.on('error', reject);
    if (body) request.write(body);
    request.end();
  });
}

async function assertMockBridge() {
  const shelf = await bridgeRequest('POST', '/api/shelf', {});
  if (!Array.isArray(shelf.books) || shelf.books.length < 1) throw new Error('mock shelf is empty');

  const cookieAuth = await bridgeRequest('POST', '/api/auth/manual', { cookie: 'wr_vid=mock-vid; wr_skey=mock-skey' });
  if (!cookieAuth.state || !cookieAuth.state.hasWebCookie) throw new Error('mock Web Cookie was not accepted');
  const cookieShelf = await bridgeRequest('POST', '/api/shelf', {});
  if (!Array.isArray(cookieShelf.books) || cookieShelf.books.length < 1) throw new Error('mock Cookie-only shelf is empty');

  const catalog = await bridgeRequest('POST', '/api/reader/catalog', { bookId: 'wf-001' });
  if (!Array.isArray(catalog.chapters) || catalog.chapters.length !== 3) throw new Error('mock catalog is incomplete');

  const chapter = await bridgeRequest('POST', '/api/reader/chapter', { bookId: 'wf-001', chapterUid: 1 });
  if (typeof chapter !== 'string' || !chapter.includes('合成章节正文')) throw new Error('mock chapter content is missing');

  const report = await bridgeRequest('POST', '/api/reader/report', {
    bookId: 'wf-001', chapterUid: 1, chapterIdx: 1, chapterOffset: 60,
    progress: 12, summary: '合成章节正文', elapsed: 60,
  });
  if (!report || Number(report.succ) !== 1) throw new Error('mock reading report failed');

  const stats = await bridgeRequest('POST', '/api/stats', {});
  if (!stats.stats || !stats.notes) throw new Error('mock reading data is missing');
  const statsGet = await bridgeRequest('GET', '/api/stats');
  if (!statsGet.stats || !statsGet.notes) throw new Error('GET mock reading data is missing');
  const weekly = await bridgeRequest('GET', '/api/stats?mode=weekly');
  if (weekly.mode !== 'weekly' || weekly.stats.mode !== 'weekly') throw new Error('weekly cloud stats mode was not forwarded');
  const remote = await bridgeRequest('GET', '/api/reader/progress?bookId=wf-001');
  if (String(remote.chapterUid) !== '1') throw new Error('mock progress pull failed');
  const highlight = await bridgeRequest('POST', '/api/reader/highlight', {
    bookId: 'wf-001', chapterUid: 1, range: '4-10', markText: '合成当前行', style: 1,
  });
  if (Number(highlight.succ) !== 1) throw new Error('mock cloud highlight failed');
  const thought = await bridgeRequest('POST', '/api/reader/thought', {
    bookId: 'wf-001', chapterUid: 1, range: '4-10', abstract: '合成当前行', content: '合成想法', type: 1,
  });
  if (Number(thought.succ) !== 1) throw new Error('mock cloud thought failed');

  const qr = await bridgeRequest('POST', '/api/qr/begin', {});
  const login = await bridgeRequest('POST', '/api/qr/poll', { uid: qr.uid });
  if (!login.login || !login.login.succeed) throw new Error('mock QR exchange failed');
}

async function assertMockRenderer() {
  if (panel.isVisible()) throw new Error('reader panel is visible at startup');
  await Promise.race([
    rendererReady,
    new Promise((_, reject) => setTimeout(() => reject(new Error('renderer did not load')), 8000)),
  ]);
  let snapshot = null;
  for (let attempt = 0; attempt < 40; attempt += 1) {
    const candidate = await panel.webContents.executeJavaScript(`window.__wefishTest ? window.__wefishTest.openMock() : null`, true);
    snapshot = candidate || snapshot;
    if (candidate && candidate.pageCount >= 2) break;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  if (!snapshot || snapshot.pageCount < 2) throw new Error(`renderer pagination did not produce multiple pages: ${JSON.stringify(snapshot)}`);
  if (!snapshot.ringDashoffset || Number(snapshot.ringDashoffset) <= 0) throw new Error('renderer progress ring did not draw a partial arc');
  if (snapshot.overflow !== 'hidden') throw new Error(`renderer scroll is enabled: ${snapshot.overflow}`);
  if (snapshot.resumeOffset < 40 || !snapshot.lineProgress || snapshot.lineProgress.startsWith('1 /')) {
    throw new Error(`renderer did not restore the remote in-page position: ${JSON.stringify(snapshot)}`);
  }
  if (!Array.isArray(snapshot.preloadedPages) || snapshot.preloadedPages.length < 2) {
    throw new Error('renderer did not preload the next pages');
  }
  if (!snapshot.lineProgress || snapshot.lineProgress === '1 / 1 行' || !snapshot.lineProgress.includes('/')) {
    throw new Error(`renderer line progress is page-local: ${snapshot.lineProgress}`);
  }
  const next = await panel.webContents.executeJavaScript('window.__wefishTest.nextPage()', true);
  if (next.pageIndex !== Math.min(snapshot.pageCount - 1, snapshot.pageIndex + 1)) throw new Error('renderer next-page action failed');
  const line = await panel.webContents.executeJavaScript('window.__wefishTest.nextLine()', true);
  if (!line.line) throw new Error('renderer line ticker did not produce text');
  const jumped = await panel.webContents.executeJavaScript('window.__wefishTest.jumpToProgress(50)', true);
  if (jumped.chapterIndex !== 1 || jumped.pageIndex < 1) throw new Error(`renderer position jump failed: ${JSON.stringify(jumped)}`);
  await new Promise((resolve) => setTimeout(resolve, 50));
  if (!trayLineText) throw new Error('tray line IPC did not receive text');
  if (!trayProgressTitle.includes('行') || !trayProgressTitle.includes('/') || trayProgressTitle.includes('%')) throw new Error('tray progress text duplicated the graphical percentage');
  if (process.platform === 'darwin' && (!progressTray || !progressTrayHasRing)) {
    throw new Error('macOS progress tray did not receive a graphical ring');
  }
  const integration = await new Promise((resolve, reject) => {
    const request = http.get(`http://127.0.0.1:${INTEGRATION_PORT}/api/reader-state`, (response) => {
      let body = '';
      response.setEncoding('utf8');
      response.on('data', (chunk) => { body += chunk; });
      response.on('end', () => {
        try { resolve({ status: response.statusCode, payload: JSON.parse(body) }); }
        catch (error) { reject(error); }
      });
    });
    request.setTimeout(2000, () => request.destroy(new Error('Edge integration self-test timed out')));
    request.on('error', reject);
  });
  if (integration.status !== 200 || integration.payload?.rawText === undefined || integration.payload?.lineProgress === undefined) {
    throw new Error('Edge integration state endpoint is incomplete');
  }
  const requiredMenuShortcuts = [
    ['显示 / 隐藏阅读舱', SHORTCUTS.togglePanel],
    ['同步书架', SHORTCUTS.sync],
    ['上一页', SHORTCUTS.pagePrevious],
    ['下一页', SHORTCUTS.pageNext],
    ['上一章', SHORTCUTS.chapterPrevious],
    ['下一章', SHORTCUTS.chapterNext],
    ['上一行', SHORTCUTS.linePrevious],
    ['下一行', SHORTCUTS.lineNext],
    ['自动逐行', SHORTCUTS.lineToggle],
    ['快捷键与使用说明', SHORTCUTS.help],
    ['小窗模式', SHORTCUTS.compact],
    ['工作文档标题', SHORTCUTS.officeTitle],
  ];
  for (const [label, accelerator] of requiredMenuShortcuts) {
    const item = trayMenu?.items.find((candidate) => candidate.label === label);
    if (!item || item.accelerator !== accelerator) throw new Error(`tray shortcut is missing: ${label}`);
  }
  const contentMenu = trayMenu?.items.find((candidate) => candidate.label === '当前内容');
  const contentLabels = contentMenu?.submenu?.items.map((item) => item.label) || [];
  for (const label of ['复制当前行', '复制当前段', '划线当前行', '划线当前段', '写想法']) {
    if (!contentLabels.includes(label)) throw new Error(`tray content action is missing: ${label}`);
  }
  if (isWindows) {
    await miniSurfaceReady;
    if (!miniSurface || !miniSurface.isVisible()) throw new Error('Windows reading surface is not visible');
    const miniText = await miniSurface.webContents.executeJavaScript('document.querySelector("#mini-line").textContent', true);
    if (!miniText) throw new Error('Windows reading surface has no current line');
    setCompactMode(false);
    if (miniSurface.isVisible()) throw new Error('Windows reading surface did not hide');
    setCompactMode(true);
    if (!miniSurface.isVisible()) throw new Error('Windows reading surface did not restore');
  }
  const widthSnapshots = await panel.webContents.executeJavaScript('window.__wefishTest.checkTrayWidths()', true);
  if (!Array.isArray(widthSnapshots) || widthSnapshots.length !== 3 || widthSnapshots.some((value) => (
    value.storedMode !== value.mode || Number(value.storedChars) !== value.chars
  ))) throw new Error(`tray width settings did not persist: ${JSON.stringify(widthSnapshots)}`);
  const insights = await panel.webContents.executeJavaScript('window.__wefishTest.openInsights()', true);
  if (!insights.includes('本周阅读时长') || !insights.includes('本月阅读时长') || !insights.includes('会员权益') || insights.includes('未知接口')) {
    throw new Error('renderer reading-data panel did not use the working GET contract');
  }
  const chrome = await panel.webContents.executeJavaScript(`({
    hasBrandName: document.body.textContent.includes('上班墨鱼') || document.body.textContent.includes('WEFISH'),
    hasPageShortcuts: document.body.textContent.includes('上一页 / 下一页'),
    hasLineShortcuts: document.body.textContent.includes('上一行 / 下一行'),
    hasAutoLineShortcut: document.body.textContent.includes('自动逐行'),
    hasHideShortcut: document.body.textContent.includes('隐藏 / 唤起'),
    hasShelfSearch: Boolean(document.querySelector('#shelf-search')),
    hasRecentShelf: Boolean(document.querySelector('#recent-section'))
      && Boolean(document.querySelector('#recent-list')),
    hasProgressRing: Boolean(document.querySelector('#progress-ring-value'))
      && Boolean(document.querySelector('#progress-percent'))
      && Boolean(document.querySelector('#progress-line-count')),
    hasProgressSettings: Boolean(document.querySelector('#show-line-progress'))
      && Boolean(document.querySelector('#show-percent-progress')),
    hasTrayWidthSettings: document.body.textContent.includes('顶部栏阅读区域')
      && Boolean(document.querySelector('#tray-width-mode'))
      && Boolean(document.querySelector('#tray-width-number')),
  })`, true);
  if (chrome.hasBrandName) throw new Error('reader page exposes the product name');
  if (!chrome.hasPageShortcuts || !chrome.hasLineShortcuts || !chrome.hasAutoLineShortcut || !chrome.hasHideShortcut || !chrome.hasShelfSearch || !chrome.hasRecentShelf || !chrome.hasProgressRing || !chrome.hasProgressSettings || !chrome.hasTrayWidthSettings) {
    throw new Error('reader shortcut guide is incomplete');
  }
  const shelfSearch = await panel.webContents.executeJavaScript('window.__wefishTest.checkShelfSearch()', true);
  if (!shelfSearch || shelfSearch.matched !== 1 || shelfSearch.recentCount !== 1 || shelfSearch.total !== 2) {
    throw new Error(`shelf search did not filter locally: ${JSON.stringify(shelfSearch)}`);
  }
  openThoughtWindow();
  await new Promise((resolve, reject) => {
    if (!thoughtWindow || thoughtWindow.isDestroyed()) return reject(new Error('thought window was not created'));
    if (!thoughtWindow.webContents.isLoading()) return resolve();
    const timer = setTimeout(() => reject(new Error('thought window did not load')), 4000);
    thoughtWindow.webContents.once('did-finish-load', () => { clearTimeout(timer); resolve(); });
  });
  const thoughtReady = await thoughtWindow.webContents.executeJavaScript('Boolean(document.querySelector("#thought")) && document.body.textContent.includes("Ctrl+Enter")', true);
  if (!thoughtReady) throw new Error('thought window input contract is incomplete');
  thoughtWindow.hide();
  if (panel.isVisible()) throw new Error('reader panel was shown by menu-bar reading actions');
  return snapshot;
}

if (hasInstanceLock) {
  app.on('second-instance', () => {
    if (!panel) return;
    setPanelVisible(true);
    panel.focus();
  });

  app.whenReady().then(async () => {
    if (process.platform === 'darwin' && app.dock) app.dock.hide();
    createPanel();
    createThoughtWindow();
    createWindowsReadingSurface();
    createProgressTray();
    // macOS inserts a later status item on the left. Create the progress item
    // first so the reading title remains left and the ring stays far right.
    createTray();
    if (progressTray && trayMenu) progressTray.setContextMenu(trayMenu);
    startIntegrationServer();
    ipcMain.handle('bridge-url', () => bridgeUrl);
    ipcMain.on('line-ticker', (_event, value) => setTrayLine(value));
    ipcMain.on('current-context', (_event, value) => {
      if (!value || typeof value !== 'object') return;
      currentContext = { ...currentContext, ...value };
    });
    ipcMain.on('thought-submit', (_event, value) => {
      if (thoughtWindow && !thoughtWindow.isDestroyed()) thoughtWindow.hide();
      dispatchCommand({ type: 'thought-submit', text: String(value || '') });
    });
    ipcMain.on('thought-cancel', () => {
      if (thoughtWindow && !thoughtWindow.isDestroyed()) thoughtWindow.hide();
    });
    ipcMain.on('mini-ready', () => updateMiniSurface(miniSnapshot));
    ipcMain.on('hide-panel', () => setPanelVisible(false));
    ipcMain.on('show-panel', () => setPanelVisible(true));
    ipcMain.on('reading-flushed', () => {
      if (!quitInProgress) return;
      stopping = true;
      if (bridge && !bridge.killed) bridge.kill();
      app.exit(0);
    });
    try {
      await startBridge();
      if (selfTestMode) {
        await assertBridgeHealth();
        if (mockMode) {
          await assertMockBridge();
          const renderer = await assertMockRenderer();
          console.log(`WEFISH_RENDERER_SELF_TEST=ok pages=${renderer.pageCount} preloaded=${renderer.preloadedPages.length}`);
        }
        console.log('WEFISH_SELF_TEST=ok');
        app.quit();
        return;
      }
      registerShortcuts();
    } catch (error) {
      if (process.env.WEFISH_DEBUG) console.error(`WeFish bridge unavailable: ${error.message || error}`);
      if (selfTestMode) {
        console.error(`WEFISH_SELF_TEST=failed: ${error.message || error}`);
        app.exit(1);
        return;
      }
      panel?.webContents.once('did-finish-load', () => panel.webContents.send('bridge-error', String(error.message || error)));
    }
  });
}

app.on('window-all-closed', (event) => event.preventDefault());
app.on('before-quit', (event) => {
  if (!selfTestMode && !quitInProgress && panel && !panel.isDestroyed()) {
    event.preventDefault();
    quitInProgress = true;
    panel.webContents.send('flush-reading');
    setTimeout(() => {
      if (quitInProgress) {
        stopping = true;
        if (bridge && !bridge.killed) bridge.kill();
        app.exit(0);
      }
    }, 2000);
    return;
  }
  stopping = true;
  globalShortcut.unregisterAll();
  if (bridge && !bridge.killed) bridge.kill();
  if (integrationServer && integrationServer.listening) integrationServer.close();
  if (progressTray && !progressTray.isDestroyed()) progressTray.destroy();
});
