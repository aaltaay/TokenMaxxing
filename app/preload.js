'use strict';

const { contextBridge, ipcRenderer } = require('electron');

// The renderer gets a narrow, named surface -- no ipcRenderer, no node.
contextBridge.exposeInMainWorld('hud', {
  call: (cmd, args) => ipcRenderer.invoke('engine:call', cmd, args),
  engineStatus: () => ipcRenderer.invoke('engine:status'),
  restartEngine: () => ipcRenderer.invoke('engine:restart'),

  windowState: () => ipcRenderer.invoke('win:state'),
  setAlwaysOnTop: (value) => ipcRenderer.invoke('win:always-on-top', value),
  requestAttention: () => ipcRenderer.invoke('win:attention'),

  openPath: (target) => ipcRenderer.invoke('shell:open-path', target),
  showItem: (target) => ipcRenderer.invoke('shell:show-item', target),
  openExternal: (url) => ipcRenderer.invoke('shell:open-external', url),

  on: (event, handler) => {
    const channels = { ready: 'engine:ready', down: 'engine:down', fatal: 'engine:fatal' };
    const channel = channels[event];
    if (!channel) throw new Error(`unknown event: ${event}`);
    const listener = (_e, payload) => handler(payload);
    ipcRenderer.on(channel, listener);
    return () => ipcRenderer.removeListener(channel, listener);
  },
});
