'use strict';

const { contextBridge, ipcRenderer } = require('electron');

// The renderer gets a narrow, named surface -- no ipcRenderer, no node.
contextBridge.exposeInMainWorld('hud', {
  call: (cmd, args) => ipcRenderer.invoke('engine:call', cmd, args),
  engineStatus: () => ipcRenderer.invoke('engine:status'),
  updateStatus: () => ipcRenderer.invoke('updates:status'),
  checkUpdates: () => ipcRenderer.invoke('updates:check'),
  installUpdate: () => ipcRenderer.invoke('updates:install'),
  previewResetAlert: () => ipcRenderer.invoke('reset:preview'),
  resetAlertStatus: () => ipcRenderer.invoke('reset:status'),
  dismissResetAlert: () => ipcRenderer.invoke('reset:dismiss'),
  smsStatus: () => ipcRenderer.invoke('sms:status'),
  saveSms: config => ipcRenderer.invoke('sms:save', config),
  testSms: () => ipcRenderer.invoke('sms:test'),
  restartEngine: () => ipcRenderer.invoke('engine:restart'),
  connectProvider: (provider) => ipcRenderer.invoke('provider:connect', provider),
  providerLinkStatus: (provider) => ipcRenderer.invoke('provider:link-status', provider),
  cancelProviderLink: (provider) => ipcRenderer.invoke('provider:cancel', provider),
  reopenProviderLogin: (provider) => ipcRenderer.invoke('provider:open-login', provider),
  openProviderInstall: (provider) => ipcRenderer.invoke('provider:open-install', provider),

  windowState: () => ipcRenderer.invoke('win:state'),
  setAlwaysOnTop: (value) => ipcRenderer.invoke('win:always-on-top', value),
  requestAttention: () => ipcRenderer.invoke('win:attention'),

  openPath: (target) => ipcRenderer.invoke('shell:open-path', target),
  showItem: (target) => ipcRenderer.invoke('shell:show-item', target),
  openExternal: (url) => ipcRenderer.invoke('shell:open-external', url),

  on: (event, handler) => {
    const channels = { ready: 'engine:ready', down: 'engine:down', fatal: 'engine:fatal', link: 'provider:link', connectionsChanged: 'providers:changed', resetAlert: 'reset:alert', smsStatus: 'sms:status' };
    const channel = event === 'updateStatus' ? 'updates:status' : channels[event];
    if (!channel) throw new Error(`unknown event: ${event}`);
    const listener = (_e, payload) => handler(payload);
    ipcRenderer.on(channel, listener);
    return () => ipcRenderer.removeListener(channel, listener);
  },
});
