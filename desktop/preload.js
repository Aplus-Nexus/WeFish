const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('wefish', {
  bridgeUrl: () => ipcRenderer.invoke('bridge-url'),
  setLine: (text) => ipcRenderer.send('line-ticker', String(text || '')),
  hide: () => ipcRenderer.send('hide-panel'),
  show: () => ipcRenderer.send('show-panel'),
  onCommand: (callback) => ipcRenderer.on('command', (_event, command) => callback(command)),
  onVisibility: (callback) => ipcRenderer.on('panel-visibility', (_event, visible) => callback(visible)),
  onBridgeError: (callback) => ipcRenderer.on('bridge-error', (_event, message) => callback(message)),
  onFlushReading: (callback) => ipcRenderer.on('flush-reading', () => callback()),
  readingFlushed: () => ipcRenderer.send('reading-flushed'),
});
