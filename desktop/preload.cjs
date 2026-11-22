'use strict';
const {contextBridge, ipcRenderer} = require('electron');
const allowed = new Set(['status','bootstrap','connect','setup/save','start','stop','activity','diagnostic','provider/save','provider/test','provider/discover','dashboard','inventory/save','inventory/parse','market/snapshot','market/refresh','market/save','assistant/ask','assistant/history','settings/get','settings/save','alerts/list','alerts/action','sheet/link','sheet/open','live/start','live/audio','live/text','live/stop','live/mic-end','fx/convert','fx/currencies','fx/world','fx/history','inventory/bulk','notifications/test','provider/reveal','profile/sync','sheet/prepare','bridge/open','extension/pair','bridge/pair','window/theme','window/title','window/zoom','voice/turn','voice/speak','voice/voices','voice/arm','voice/disarm','wa/connect','wa/disconnect','wa/status','wa/sync','wa/scan-mode','outbound/list','outbound/action','contacts/list','contacts/tag','contacts/unmute','contacts/mute-status','scheduler/list','outbound/create','diagnostics/logs','diagnostics/clear','diagnostics/save-log','inventory/daily-stock','outbound/dispatch','attachment/stage','clipboard/read-image','message/trace']);
contextBridge.exposeInMainWorld('jarvis', Object.freeze({
  call: async (method, params = {}) => {
    if (!allowed.has(method)) throw new Error('Unsupported action');
    const response = await ipcRenderer.invoke('jarvis:request', {method, params});
    if (response.error) {
      const error = new Error(response.error.message);
      error.code = response.error.code;
      throw error;
    }
    return response.result;
  },
  onServiceState: (callback) => {
    if (typeof callback !== 'function') throw new TypeError('A callback is required');
    const handler = (_event, state) => callback(state);
    ipcRenderer.on('jarvis:service-state', handler);
    return () => ipcRenderer.removeListener('jarvis:service-state', handler);
  },
  onEvent: callback => {
    if(typeof callback!=='function')throw new TypeError('Callback required');
    const handler=(_event,packet)=>callback(packet);ipcRenderer.on('jarvis:event',handler);
    return ()=>ipcRenderer.removeListener('jarvis:event',handler);
  }
}));





