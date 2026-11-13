const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('wefish', {
  bridgeUrl: () => ipcRenderer.invoke('bridge-url'),
  setLine: (value) => {
    if (value && typeof value === 'object') {
      ipcRenderer.send('line-ticker', {
        text: String(value.text || ''),
        rawText: String(value.rawText || value.text || ''),
        paragraph: String(value.paragraph || value.rawText || value.text || ''),
        widthMode: String(value.widthMode || 'dynamic'),
        widthChars: Number(value.widthChars || 28),
        bookId: String(value.bookId || ''),
        bookTitle: String(value.bookTitle || ''),
        chapterUid: String(value.chapterUid || ''),
        chapterName: String(value.chapterName || ''),
        range: String(value.range || '0-0'),
        paragraphRange: String(value.paragraphRange || value.range || '0-0'),
        lineProgress: String(value.lineProgress || ''),
        percent: Number(value.percent || 0),
        ringPng: String(value.ringPng || ''),
        showLineProgress: value.showLineProgress !== false,
        showPercent: value.showPercent !== false,
      });
      return;
    }
    ipcRenderer.send('line-ticker', String(value || ''));
  },
  hide: () => ipcRenderer.send('hide-panel'),
  show: () => ipcRenderer.send('show-panel'),
  onCommand: (callback) => ipcRenderer.on('command', (_event, command) => callback(command)),
  sendCurrentContext: (value) => ipcRenderer.send('current-context', value || {}),
  onVisibility: (callback) => ipcRenderer.on('panel-visibility', (_event, visible) => callback(visible)),
  onMiniUpdate: (callback) => ipcRenderer.on('mini-update', (_event, value) => callback(value)),
  miniReady: () => ipcRenderer.send('mini-ready'),
  onBridgeError: (callback) => ipcRenderer.on('bridge-error', (_event, message) => callback(message)),
  onFlushReading: (callback) => ipcRenderer.on('flush-reading', () => callback()),
  readingFlushed: () => ipcRenderer.send('reading-flushed'),
  onThoughtContext: (callback) => ipcRenderer.on('thought-context', (_event, value) => callback(value)),
  submitThought: (value) => ipcRenderer.send('thought-submit', String(value || '')),
  cancelThought: () => ipcRenderer.send('thought-cancel'),
});
