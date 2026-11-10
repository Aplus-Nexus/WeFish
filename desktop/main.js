const { app, BrowserWindow, Tray, Menu, nativeImage, globalShortcut, ipcMain, screen, dialog } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const http = require('http');
const path = require('path');

const mockMode = process.argv.includes('--mock');
const selfTestMode = process.argv.includes('--self-test');
const showOnStart = process.argv.includes('--show');
const hasInstanceLock = app.requestSingleInstanceLock();
let panel = null;
let tray = null;
let bridge = null;
let bridgeUrl = '';
let stopping = false;
let officeTitleMode = false;
let officeTitleMenuItem = null;
let compactMode = false;
let compactMenuItem = null;
let quitInProgress = false;
let trayLineText = '';
let panelPlaced = false;
let rendererReady = Promise.resolve();

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
  const height = compactMode ? 270 : 650;
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

function setOfficeTitleMode(enabled) {
  officeTitleMode = Boolean(enabled);
  panel?.setTitle(officeTitleMode ? '工作文档' : '阅读器');
  tray?.setToolTip(officeTitleMode ? '工作文档' : (trayLineText || '阅读器'));
  if (officeTitleMenuItem) officeTitleMenuItem.checked = officeTitleMode;
  if (!officeTitleMode) setTrayLine(trayLineText);
}

function setTrayLine(value) {
  // Keep the renderer's non-breaking padding: macOS uses the title width to
  // reserve the visible menu-bar reading area.
  trayLineText = String(value || '')
    .replace(/[\t\n\r ]+/g, ' ')
    .replace(/^ +| +$/g, '')
    .slice(0, 160);
  if (!tray || officeTitleMode) return;
  if (process.platform === 'darwin' && typeof tray.setTitle === 'function') {
    tray.setTitle(trayLineText);
  }
  tray.setToolTip(trayLineText || '阅读器');
}

function setCompactMode(enabled) {
  compactMode = Boolean(enabled);
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
    if (panel && panel.isVisible() && !compactMode && !panel.webContents.isDevToolsOpened()) setPanelVisible(false);
  });
  panel.on('closed', () => { panel = null; });
}

function createTray() {
  tray = new Tray(trayImage());
  tray.setToolTip('阅读器');
  tray.on('click', togglePanel);
  const menu = Menu.buildFromTemplate([
    { label: '显示 / 隐藏阅读舱', click: togglePanel },
    { type: 'separator' },
    { label: '同步书架', click: () => panel?.webContents.send('command', 'sync') },
    { label: '上一页', click: () => panel?.webContents.send('command', 'page-previous') },
    { label: '下一页', click: () => panel?.webContents.send('command', 'page-next') },
    { label: '上一章', click: () => panel?.webContents.send('command', 'chapter-previous') },
    { label: '下一章', click: () => panel?.webContents.send('command', 'chapter-next') },
    { label: '上一行', click: () => panel?.webContents.send('command', 'line-previous') },
    { label: '下一行', click: () => panel?.webContents.send('command', 'line-next') },
    { label: '自动逐行', click: () => panel?.webContents.send('command', 'line-toggle') },
    { label: '逐行速度', submenu: [
      { label: '0.5 秒（快）', click: () => panel?.webContents.send('command', 'line-interval:0.5') },
      { label: '1 秒（默认）', click: () => panel?.webContents.send('command', 'line-interval:1') },
      { label: '2 秒', click: () => panel?.webContents.send('command', 'line-interval:2') },
      { label: '3 秒', click: () => panel?.webContents.send('command', 'line-interval:3') },
      { label: '5 秒（慢）', click: () => panel?.webContents.send('command', 'line-interval:5') },
    ] },
    { label: '顶部栏阅读区域', submenu: [
      { label: '跟随文本（动态）', click: () => panel?.webContents.send('command', 'tray-width:dynamic') },
      { label: '固定 28 字', click: () => panel?.webContents.send('command', 'tray-width:fixed:28') },
      { label: '固定 40 字', click: () => panel?.webContents.send('command', 'tray-width:fixed:40') },
      { label: '固定 56 字', click: () => panel?.webContents.send('command', 'tray-width:fixed:56') },
      { label: '尽量占满空余位置', click: () => panel?.webContents.send('command', 'tray-width:fill') },
      { type: 'separator' },
      { label: '打开宽度设置', click: () => { setPanelVisible(true); panel?.webContents.send('command', 'tray-width:settings'); } },
    ] },
    { label: '快捷键与使用说明', click: () => { setPanelVisible(true); panel?.webContents.send('command', 'help'); } },
    { label: '微信扫码', click: () => { setPanelVisible(true); panel?.webContents.send('command', 'qr'); } },
    { id: 'compact-mode', label: '小窗模式', type: 'checkbox', click: (item) => setCompactMode(item.checked) },
    { id: 'office-title-mode', label: '工作文档标题', type: 'checkbox', click: (item) => setOfficeTitleMode(item.checked) },
    { type: 'separator' },
    { label: '关于 / 许可证', click: () => dialog.showMessageBox(panel || undefined, {
      type: 'info',
      title: '上班墨鱼 · WeFish',
      message: '上班墨鱼 WeFish',
      detail: '本程序基于 finlater/weread.koplugin 的协议处理与内容解码思路改编。\n\n本项目及改编部分按 GNU Affero General Public License 3.0 发布；不提供任何保证。安装包内的 licenses/LICENSE 与 licenses/NOTICE 保存完整许可证和来源说明，项目源码包含对应源码。\n\nWeFish 与微信读书、腾讯或 KOReader 没有隶属、背书或赞助关系。',
      buttons: ['知道了'],
    }) },
    { label: '退出阅读器', click: () => app.quit() },
  ]);
  compactMenuItem = menu.getMenuItemById('compact-mode');
  officeTitleMenuItem = menu.getMenuItemById('office-title-mode');
  tray.setContextMenu(menu);
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
  return { command: process.env.WEFISH_PYTHON || 'python3', args: [path.join(__dirname, '..', 'wefish_service.py')] };
}

function startBridge() {
  return new Promise((resolve, reject) => {
    const command = bridgeCommand();
    const args = [...command.args, '--port', '0'];
    if (mockMode) args.push('--mock');
    bridge = spawn(command.command, args, { cwd: path.join(__dirname, '..'), env: { ...process.env }, stdio: ['ignore', 'pipe', 'pipe'] });
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
  const modifier = 'CommandOrControl+Alt+';
  globalShortcut.register(`${modifier}M`, togglePanel);
  globalShortcut.register(`${modifier}R`, () => panel?.webContents.send('command', 'sync'));
  globalShortcut.register(`${modifier}Left`, () => panel?.webContents.send('command', 'page-previous'));
  globalShortcut.register(`${modifier}Right`, () => panel?.webContents.send('command', 'page-next'));
  globalShortcut.register(`${modifier}Shift+Left`, () => panel?.webContents.send('command', 'chapter-previous'));
  globalShortcut.register(`${modifier}Shift+Right`, () => panel?.webContents.send('command', 'chapter-next'));
  globalShortcut.register(`${modifier}Up`, () => panel?.webContents.send('command', 'line-previous'));
  globalShortcut.register(`${modifier}Down`, () => panel?.webContents.send('command', 'line-next'));
  globalShortcut.register(`${modifier}Space`, () => panel?.webContents.send('command', 'line-toggle'));
  globalShortcut.register(`${modifier}T`, () => setOfficeTitleMode(!officeTitleMode));
  globalShortcut.register(`${modifier}P`, () => setCompactMode(!compactMode));
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
  const remote = await bridgeRequest('GET', '/api/reader/progress?bookId=wf-001');
  if (String(remote.chapterUid) !== '1') throw new Error('mock progress pull failed');

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
  if (snapshot.overflow !== 'hidden') throw new Error(`renderer scroll is enabled: ${snapshot.overflow}`);
  if (snapshot.resumeOffset < 40 || snapshot.lineIndex < 1) {
    throw new Error(`renderer did not restore the remote in-page position: ${JSON.stringify(snapshot)}`);
  }
  if (!Array.isArray(snapshot.preloadedPages) || snapshot.preloadedPages.length < 2) {
    throw new Error('renderer did not preload the next pages');
  }
  const next = await panel.webContents.executeJavaScript('window.__wefishTest.nextPage()', true);
  if (next.pageIndex !== 1) throw new Error('renderer next-page action failed');
  const line = await panel.webContents.executeJavaScript('window.__wefishTest.nextLine()', true);
  if (!line.line) throw new Error('renderer line ticker did not produce text');
  const jumped = await panel.webContents.executeJavaScript('window.__wefishTest.jumpToProgress(50)', true);
  if (jumped.chapterIndex !== 1 || jumped.pageIndex < 1) throw new Error(`renderer position jump failed: ${JSON.stringify(jumped)}`);
  await new Promise((resolve) => setTimeout(resolve, 50));
  if (!trayLineText) throw new Error('tray line IPC did not receive text');
  const insights = await panel.webContents.executeJavaScript('window.__wefishTest.openInsights()', true);
  if (!insights.includes('阅读时长') || !insights.includes('会员权益') || insights.includes('未知接口')) {
    throw new Error('renderer reading-data panel did not use the working GET contract');
  }
  const chrome = await panel.webContents.executeJavaScript(`({
    hasBrandName: document.body.textContent.includes('上班墨鱼') || document.body.textContent.includes('WEFISH'),
    hasPageShortcuts: document.body.textContent.includes('上一页 / 下一页'),
    hasLineShortcuts: document.body.textContent.includes('上一行 / 下一行'),
    hasAutoLineShortcut: document.body.textContent.includes('自动逐行'),
    hasHideShortcut: document.body.textContent.includes('隐藏 / 唤起'),
    hasTrayWidthSettings: document.body.textContent.includes('顶部栏阅读区域')
      && Boolean(document.querySelector('#tray-width-mode'))
      && Boolean(document.querySelector('#tray-width-number')),
  })`, true);
  if (chrome.hasBrandName) throw new Error('reader page exposes the product name');
  if (!chrome.hasPageShortcuts || !chrome.hasLineShortcuts || !chrome.hasAutoLineShortcut || !chrome.hasHideShortcut || !chrome.hasTrayWidthSettings) {
    throw new Error('reader shortcut guide is incomplete');
  }
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
    createTray();
    ipcMain.handle('bridge-url', () => bridgeUrl);
    ipcMain.on('line-ticker', (_event, value) => setTrayLine(value));
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
});
