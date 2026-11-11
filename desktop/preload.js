const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('wefish', {
  bridgeUrl: () => ipcRenderer.invoke('bridge-url'),
  setLine: (value) => {
    if (value && typeof value === 'object') {
      ipcRenderer.send('line-ticker', {
        text: String(value.text || ''),
        rawText: String(value.rawText || value.text || ''),
        widthMode: String(value.widthMode || 'dynamic'),
        widthChars: Number(value.widthChars || 28),
      });
      return;
    }
    ipcRenderer.send('line-ticker', String(value || ''));
  },
  hide: () => ipcRenderer.send('hide-panel'),
  show: () => ipcRenderer.send('show-panel'),
  onCommand: (callback) => ipcRenderer.on('command', (_event, command) => callback(command)),
  onVisibility: (callback) => ipcRenderer.on('panel-visibility', (_event, visible) => callback(visible)),
  onMiniUpdate: (callback) => ipcRenderer.on('mini-update', (_event, value) => callback(value)),
  miniReady: () => ipcRenderer.send('mini-ready'),
  onBridgeError: (callback) => ipcRenderer.on('bridge-error', (_event, message) => callback(message)),
  onFlushReading: (callback) => ipcRenderer.on('flush-reading', () => callback()),
  readingFlushed: () => ipcRenderer.send('reading-flushed'),
});
